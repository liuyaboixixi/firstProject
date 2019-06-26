package com.javaproject.demo.model;

import lombok.Data;

@Data
public class Review {
    private Long  id;
    private Long  parentId;
    private Integer  type;
    private Integer commentator;
    private Long gmtCreate;
    private Long gmtModified;
    private Long likeCount;
    private String content;
}
